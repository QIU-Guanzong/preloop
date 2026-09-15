"""Execution lineage: the parent/root/depth columns and the children lookup.

Child of the flows delegation work (#621). The columns land with no writer, so
these tests pin the shape rather than any triggering behaviour: existing rows
read back as roots, the parent id is a real foreign key, creation stores what
it is given, and the children lookup is direct-only and stable.
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.models.crud import crud_flow_execution
from preloop.models.crud.audit_chain import FOREIGN_KEY_VIOLATION, postgres_sqlstate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20260915_execution_lineage.py"
)
LINEAGE_COLUMNS = {"parent_execution_id", "root_execution_id", "delegation_depth"}


def _migration():
    """Load the lineage revision off disk, as the sibling migration tests do."""
    spec = importlib.util.spec_from_file_location(
        "execution_lineage_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _downgrade(db_session) -> None:
    with Operations.context(MigrationContext.configure(db_session.connection())):
        _migration().downgrade()


def _upgrade(db_session) -> None:
    with Operations.context(MigrationContext.configure(db_session.connection())):
        _migration().upgrade()


def _table_columns(db_session) -> set[str]:
    return {
        column["name"]
        for column in inspect(db_session.connection()).get_columns("flow_execution")
    }


@pytest.fixture
def flow(db_session, test_user) -> models.Flow:
    """One flow owned by the test account."""
    flow = models.Flow(
        name=f"lineage-{uuid.uuid4().hex[:8]}",
        prompt_template="t",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    db_session.add(flow)
    db_session.flush()
    return flow


def _execution(
    flow: models.Flow,
    *,
    status: str = "PENDING",
    start_time: datetime | None = None,
    parent: models.FlowExecution | None = None,
    root_id: uuid.UUID | None = None,
    depth: int = 0,
) -> models.FlowExecution:
    return models.FlowExecution(
        flow_id=flow.id,
        status=status,
        start_time=start_time or datetime.now(UTC),
        parent_execution_id=parent.id if parent else None,
        root_execution_id=root_id,
        delegation_depth=depth,
    )


# --- migration shape -------------------------------------------------------


def test_migration_backfills_existing_rows_as_roots(db_session, test_user, flow):
    """A row written before the upgrade still reads back as a root.

    The migration has to be additive: a running instance that upgrades keeps
    every execution row it already has, and those rows are their own roots
    (depth 0, no parent, no root id) until something writes lineage.
    """
    _downgrade(db_session)
    legacy_id = uuid.uuid4()
    now = datetime.now(UTC)
    db_session.execute(
        text(
            "INSERT INTO flow_execution "
            "(id, flow_id, status, start_time, created_at, updated_at, "
            " legal_hold) "
            "VALUES (:id, :flow_id, :status, :start_time, :created_at, "
            " :updated_at, false)"
        ),
        {
            "id": legacy_id,
            "flow_id": flow.id,
            "status": "SUCCEEDED",
            "start_time": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    assert not LINEAGE_COLUMNS & _table_columns(db_session)

    _upgrade(db_session)

    assert LINEAGE_COLUMNS <= _table_columns(db_session)
    # The model and the revision have to agree on the lineage columns, or the
    # next autogenerate run would try to drop what this one added.
    model_columns = {column.name for column in models.FlowExecution.__table__.columns}
    assert LINEAGE_COLUMNS <= model_columns
    legacy = db_session.execute(
        select(models.FlowExecution).filter(models.FlowExecution.id == legacy_id)
    ).scalar_one()
    assert legacy.parent_execution_id is None
    assert legacy.root_execution_id is None
    assert legacy.delegation_depth == 0


def test_migration_reverses_cleanly(db_session):
    """Downgrade removes exactly what upgrade added, indexes and FK included."""
    _downgrade(db_session)
    assert not LINEAGE_COLUMNS & _table_columns(db_session)

    _upgrade(db_session)

    assert LINEAGE_COLUMNS <= _table_columns(db_session)
    inspector = inspect(db_session.connection())
    index_names = {index["name"] for index in inspector.get_indexes("flow_execution")}
    assert "ix_flow_execution_parent_execution_id" in index_names
    assert "ix_flow_execution_root_execution_id" in index_names
    foreign_keys = inspector.get_foreign_keys("flow_execution")
    parent_fk = [
        fk
        for fk in foreign_keys
        if fk["constrained_columns"] == ["parent_execution_id"]
    ]
    assert len(parent_fk) == 1
    assert parent_fk[0]["referred_table"] == "flow_execution"
    assert parent_fk[0]["referred_columns"] == ["id"]

    depth = {
        column["name"]: column for column in inspector.get_columns("flow_execution")
    }["delegation_depth"]
    assert depth["nullable"] is False
    # The server default is what backfills existing rows and what keeps an
    # INSERT that omits the column working. Postgres reports it deparsed, so
    # match on the value rather than on a quoting style.
    assert depth["default"] is not None
    assert "0" in str(depth["default"])


def test_parent_id_must_point_at_a_real_execution(db_session, flow):
    """A dangling parent would make the tree unreadable, so the FK refuses it."""
    with pytest.raises(IntegrityError) as error:
        with db_session.begin_nested():
            db_session.add(
                models.FlowExecution(
                    flow_id=flow.id,
                    status="PENDING",
                    parent_execution_id=uuid.uuid4(),
                )
            )
            db_session.flush()
    assert postgres_sqlstate(error.value) == FOREIGN_KEY_VIOLATION


# --- CRUD ------------------------------------------------------------------


def test_create_stores_the_lineage_it_is_given(db_session, flow):
    """Creation is how a child records its parent, root and depth."""
    root = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    db_session.flush()
    child = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="RUNNING",
            parent_execution_id=root.id,
            root_execution_id=root.id,
            delegation_depth=1,
        ),
    )
    db_session.flush()

    # Read the values back from the database rather than trusting the objects
    # still in the identity map: the NOT NULL depth column and the new foreign
    # key both have to accept what creation wrote.
    db_session.expire_all()
    assert root.delegation_depth == 0
    assert root.parent_execution_id is None
    assert child.parent_execution_id == root.id
    assert child.root_execution_id == root.id
    assert child.delegation_depth == 1


def test_create_defaults_to_a_root_execution(db_session, flow):
    """Every existing creation path that does not set lineage stays a root."""
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="PENDING")
    )
    db_session.flush()
    db_session.expire_all()

    assert execution.parent_execution_id is None
    assert execution.root_execution_id is None
    assert execution.delegation_depth == 0


def test_deleting_a_parent_leaves_its_child_readable(db_session, flow):
    """The parent edge must not turn a shipped delete into a foreign-key failure.

    Deleting an execution (or a flow, which cascades to its executions) is
    existing behaviour. ``ON DELETE SET NULL`` on the parent edge, the same
    choice ``retry_of_execution_id`` made, keeps it working and leaves the
    child readable, just detached. The depth is left as recorded: it says where
    the run sat, not what still exists.
    """
    root = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    db_session.flush()
    child = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="RUNNING",
            parent_execution_id=root.id,
            root_execution_id=root.id,
            delegation_depth=1,
        ),
    )
    db_session.flush()
    child_id = child.id

    db_session.delete(root)
    db_session.flush()
    db_session.expire_all()

    detached = db_session.get(models.FlowExecution, child_id)
    assert detached is not None
    assert detached.parent_execution_id is None
    assert detached.delegation_depth == 1


def test_get_children_returns_direct_children_in_a_stable_order(db_session, flow):
    """The children lookup feeds a tree, so it must be direct-only and ordered."""
    now = datetime.now(UTC)
    root = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    db_session.flush()
    second = _execution(
        flow,
        start_time=now + timedelta(seconds=2),
        parent=root,
        root_id=root.id,
        depth=1,
    )
    first = _execution(
        flow,
        start_time=now + timedelta(seconds=1),
        parent=root,
        root_id=root.id,
        depth=1,
    )
    db_session.add_all([second, first])
    db_session.flush()
    grandchild = _execution(
        flow,
        start_time=now + timedelta(seconds=3),
        parent=first,
        root_id=root.id,
        depth=2,
    )
    db_session.add(grandchild)
    db_session.flush()

    assert [
        row.id
        for row in crud_flow_execution.get_children(
            db_session, root.id, account_id=flow.account_id
        )
    ] == [
        first.id,
        second.id,
    ]
    # A grandchild is not a direct child of the root, and a leaf is empty.
    assert [
        row.id
        for row in crud_flow_execution.get_children(
            db_session, first.id, account_id=flow.account_id
        )
    ] == [grandchild.id]
    assert (
        crud_flow_execution.get_children(
            db_session, grandchild.id, account_id=flow.account_id
        )
        == []
    )


def test_get_children_does_not_cross_accounts(db_session, flow, test_user):
    """An execution id from another account must not leak its children."""
    other_account = models.Account(organization_name="lineage-other-account")
    db_session.add(other_account)
    db_session.flush()
    other_flow = models.Flow(
        name="lineage-other-flow",
        prompt_template="t",
        agent_type="codex",
        agent_config={},
        account_id=other_account.id,
    )
    db_session.add(other_flow)
    db_session.flush()

    root = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    db_session.flush()
    mine = _execution(flow, parent=root, root_id=root.id, depth=1)
    theirs = _execution(other_flow, parent=root, root_id=root.id, depth=1)
    db_session.add_all([mine, theirs])
    db_session.flush()

    assert [
        row.id
        for row in crud_flow_execution.get_children(
            db_session, root.id, account_id=test_user.account_id
        )
    ] == [mine.id]
    assert [
        row.id
        for row in crud_flow_execution.get_children(
            db_session, root.id, account_id=other_account.id
        )
    ] == [theirs.id]
