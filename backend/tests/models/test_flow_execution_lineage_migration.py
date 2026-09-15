"""Run the lineage migration's upgrade/downgrade cycle against real Postgres.

`test_alembic_single_head.py` only walks the revision graph, and the test
database is migrated to head before the suite runs, so nothing ever exercises
this migration's downgrade. This one does, inside the test transaction: down
then up, then a row inserted while the columns did not exist is read back
through the ORM to prove the documented backfill (no parent, no root, depth
0). It also proves the self-referencing FK actually refuses an unknown parent,
which a graph test cannot say.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select

from preloop.models import models
from preloop.models.models.flow_execution import FlowExecution

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260915_flow_execution_lineage.py"
)

LINEAGE_COLUMNS = (
    "parent_execution_id",
    "root_execution_id",
    "delegation_depth",
)
PARENT_INDEX = "ix_flow_execution_parent_execution_id"
ROOT_INDEX = "ix_flow_execution_root_execution_id"
PARENT_FK = "fk_flow_execution_parent_execution_id"


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "flow_execution_lineage_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(db_session):
    """An Alembic operations context bound to the test transaction."""
    context = MigrationContext.configure(db_session.connection())
    return Operations.context(context)


def _column_names(db_session, table: str = "flow_execution") -> set[str]:
    return {
        column["name"] for column in inspect(db_session.connection()).get_columns(table)
    }


def _index_names(db_session, table: str = "flow_execution") -> set[str]:
    return {
        index["name"] for index in inspect(db_session.connection()).get_indexes(table)
    }


def _foreign_key_names(db_session, table: str = "flow_execution") -> set[str]:
    return {
        fk["name"] for fk in inspect(db_session.connection()).get_foreign_keys(table)
    }


def _make_flow(db_session, account) -> models.Flow:
    flow = models.Flow(
        name=f"Lineage Migration Flow {uuid.uuid4()}",
        prompt_template="t",
        agent_type="codex",
        agent_config={},
        account_id=account.id,
    )
    db_session.add(flow)
    db_session.flush()
    return flow


def _insert_execution_without_lineage(db_session, flow) -> uuid.UUID:
    """Insert a row the way a pre-migration writer would: no lineage columns."""
    execution_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO flow_execution "
            "(id, flow_id, status, start_time, created_at, updated_at) "
            "VALUES (:id, :flow_id, 'SUCCEEDED', now(), now(), now())"
        ),
        {"id": execution_id, "flow_id": flow.id},
    )
    db_session.flush()
    return execution_id


def test_downgrade_drops_the_lineage_columns_indexes_and_fk(db_session):
    migration = _load_migration()
    assert set(LINEAGE_COLUMNS) <= _column_names(db_session)

    with _operations(db_session):
        migration.downgrade()

    assert not set(LINEAGE_COLUMNS) & _column_names(db_session)
    assert PARENT_INDEX not in _index_names(db_session)
    assert ROOT_INDEX not in _index_names(db_session)
    assert PARENT_FK not in _foreign_key_names(db_session)


def test_upgrade_after_downgrade_rebuilds_the_orm_shape(db_session):
    """A full down/up cycle leaves exactly the columns the ORM expects."""
    migration = _load_migration()
    expected = {column.name for column in FlowExecution.__table__.columns}

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    assert _column_names(db_session) == expected
    for column in LINEAGE_COLUMNS:
        assert column in _column_names(db_session)

    columns = {
        column["name"]: column
        for column in inspect(db_session.connection()).get_columns("flow_execution")
    }
    assert columns["parent_execution_id"]["nullable"] is True
    assert columns["root_execution_id"]["nullable"] is True
    assert columns["delegation_depth"]["nullable"] is False

    assert PARENT_INDEX in _index_names(db_session)
    assert ROOT_INDEX in _index_names(db_session)
    assert PARENT_FK in _foreign_key_names(db_session)


def test_a_row_inserted_before_upgrade_reads_back_as_a_root(db_session, create_account):
    """The documented backfill: depth 0, both ids null, for existing rows."""
    migration = _load_migration()
    account = create_account()
    flow = _make_flow(db_session, account)

    with _operations(db_session):
        migration.downgrade()
    execution_id = _insert_execution_without_lineage(db_session, flow)
    with _operations(db_session):
        migration.upgrade()

    stored = db_session.execute(
        select(FlowExecution).where(FlowExecution.id == execution_id)
    ).scalar_one()

    assert stored.parent_execution_id is None
    assert stored.root_execution_id is None
    assert stored.delegation_depth == 0


def test_the_fk_refuses_a_parent_that_does_not_exist(db_session, create_account):
    """The FK is the guarantee that a recorded parent was a real execution."""
    migration = _load_migration()
    account = create_account()
    flow = _make_flow(db_session, account)

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    with pytest.raises(IntegrityError):
        db_session.add(
            models.FlowExecution(
                flow_id=flow.id,
                status="PENDING",
                parent_execution_id=uuid.uuid4(),
                root_execution_id=uuid.uuid4(),
                delegation_depth=1,
            )
        )
        db_session.flush()
    db_session.rollback()
