"""The lineage migration's down/up cycle on Postgres.

These run the revision's downgrade and upgrade against a real database,
including the case the backfill rule exists for: a row created before the
columns, which must come back with both ids null and ``delegation_depth`` 0.
The DDL itself - column shapes, indexes, the foreign key and the absence of a
backfill UPDATE - is pinned without a database in
``backend/tests/test_flow_execution_lineage_migration_ddl.py``.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from preloop.models.crud import crud_flow
from preloop.models.schemas.flow import FlowCreate

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260915_flow_execution_lineage.py"
)

LINEAGE_COLUMNS = {"parent_execution_id", "root_execution_id", "delegation_depth"}


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


def _columns(db_session, table: str) -> dict:
    return {
        column["name"]: column
        for column in inspect(db_session.connection()).get_columns(table)
    }


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


def _insert_legacy_row(db_session, flow) -> uuid.UUID:
    """A flow_execution row as it looks with the lineage columns absent."""
    execution_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO flow_execution (id, flow_id, status, start_time) "
            "VALUES (:id, :flow_id, 'SUCCEEDED', now())"
        ),
        {"id": execution_id, "flow_id": flow.id},
    )
    return execution_id


def test_downgrade_removes_the_lineage_columns(db_session):
    migration = _load_migration()
    assert LINEAGE_COLUMNS <= set(_columns(db_session, "flow_execution"))

    with _operations(db_session):
        migration.downgrade()

    assert LINEAGE_COLUMNS & set(_columns(db_session, "flow_execution")) == set()


def test_down_up_cycle_restores_the_columns_their_indexes_and_the_foreign_key(
    db_session,
):
    """A full cycle leaves the shape the model and the children lookup expect."""
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    columns = _columns(db_session, "flow_execution")
    assert LINEAGE_COLUMNS <= set(columns)
    assert columns["parent_execution_id"]["nullable"] is True
    assert columns["root_execution_id"]["nullable"] is True
    assert columns["delegation_depth"]["nullable"] is False

    inspector = inspect(db_session.connection())
    indexes = {index["name"] for index in inspector.get_indexes("flow_execution")}
    assert {
        "ix_flow_execution_parent_execution_id",
        "ix_flow_execution_root_execution_id",
    } <= indexes

    foreign_keys = inspector.get_foreign_keys("flow_execution")
    parent_fk = next(
        fk
        for fk in foreign_keys
        if fk["constrained_columns"] == ["parent_execution_id"]
    )
    assert parent_fk["referred_table"] == "flow_execution"
    assert parent_fk["options"].get("ondelete") == "SET NULL"


def test_a_row_created_before_the_upgrade_reads_null_null_zero(db_session, test_user):
    """The documented backfill rule, on a row that predates the columns."""
    migration = _load_migration()
    flow = _create_flow(db_session, test_user, "Lineage migration backfill")
    with _operations(db_session):
        migration.downgrade()
    execution_id = _insert_legacy_row(db_session, flow)
    db_session.flush()

    with _operations(db_session):
        migration.upgrade()

    row = db_session.execute(
        text(
            "SELECT parent_execution_id, root_execution_id, delegation_depth "
            "FROM flow_execution WHERE id = :id"
        ),
        {"id": execution_id},
    ).one()
    assert row.parent_execution_id is None
    assert row.root_execution_id is None
    assert row.delegation_depth == 0
