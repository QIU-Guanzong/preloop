"""Run the session hold migration's downgrade/upgrade cycle on real Postgres.

`test_alembic_single_head.py` only walks the revision graph, so nothing else
executes this migration's DDL. This does, inside the test transaction: down
then up, then check the rebuilt column against the ORM definition and check
that an existing row lands not held. An upgrade that froze an account's
sessions by default would be the opposite of what a hold means.
"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models.models.runtime_session import RuntimeSession

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260915_runtime_session_legal_hold.py"
)


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "runtime_session_legal_hold_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(db_session):
    """An Alembic operations context bound to the test transaction."""
    context = MigrationContext.configure(db_session.connection())
    return Operations.context(context)


def _column_names(db_session, table: str) -> set[str]:
    return {
        column["name"] for column in inspect(db_session.connection()).get_columns(table)
    }


def test_downgrade_removes_the_flag(db_session):
    migration = _load_migration()
    assert "legal_hold" in _column_names(db_session, "runtime_session")

    with _operations(db_session):
        migration.downgrade()

    assert "legal_hold" not in _column_names(db_session, "runtime_session")


def test_upgrade_after_downgrade_rebuilds_the_orm_shape(db_session):
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    assert "legal_hold" in _column_names(db_session, "runtime_session")
    assert RuntimeSession.__table__.columns["legal_hold"].nullable is False
    indexes = {
        index["name"]
        for index in inspect(db_session.connection()).get_indexes("runtime_session")
    }
    assert "ix_runtime_session_legal_hold" in indexes


def test_the_flag_defaults_to_false_on_existing_rows(db_session, test_user):
    """An upgrade must not freeze the sessions an account already has."""
    from datetime import UTC, datetime

    from preloop.models import models

    migration = _load_migration()
    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="managed_agent",
        session_source_id="migration-default",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()

    assert session.legal_hold is False
