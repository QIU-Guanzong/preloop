"""The lineage migration's DDL, captured without a database.

Every test under ``backend/tests/models`` needs ``DATABASE_URL`` (the
``isolate_system_wide_ai_models`` fixture is autouse), so the statements the
revision emits are pinned here instead: column shapes and nullability, both
indexes, the foreign key's delete behaviour, and the fact that the backfill
rule is the column default rather than an UPDATE over existing rows. The
downgrade/upgrade cycle against a real database is in
``backend/tests/models/test_flow_execution_lineage_migration.py``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Callable, List

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_mock_engine

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260915_flow_execution_lineage.py"
)


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "flow_execution_lineage_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(run: Callable[[], None]) -> List[str]:
    """The DDL ``run`` emits, as normalised statements, on a mock engine."""
    statements: List[str] = []
    engine = create_mock_engine(
        "postgresql://",
        lambda sql, *args, **kwargs: statements.append(
            str(sql.compile(dialect=engine.dialect))
        ),
    )
    with Operations.context(MigrationContext.configure(engine)):
        run()
    return [re.sub(r"\s+", " ", statement).strip() for statement in statements]


def test_upgrade_emits_the_columns_indexes_and_parent_foreign_key():
    migration = _load_migration()

    statements = _capture(migration.upgrade)

    assert "ALTER TABLE flow_execution ADD COLUMN parent_execution_id UUID" in (
        statements
    )
    assert "ALTER TABLE flow_execution ADD COLUMN root_execution_id UUID" in statements
    # NOT NULL DEFAULT 0 is the backfill: Postgres fills existing rows from the
    # default in the same statement, so no data migration is needed.
    assert (
        "ALTER TABLE flow_execution ADD COLUMN delegation_depth INTEGER "
        "DEFAULT 0 NOT NULL" in statements
    )
    assert (
        "CREATE INDEX ix_flow_execution_parent_execution_id ON flow_execution "
        "(parent_execution_id)" in statements
    )
    assert (
        "CREATE INDEX ix_flow_execution_root_execution_id ON flow_execution "
        "(root_execution_id)" in statements
    )
    assert (
        "ALTER TABLE flow_execution ADD CONSTRAINT "
        "fk_flow_execution_parent_execution_id FOREIGN KEY(parent_execution_id) "
        "REFERENCES flow_execution (id) ON DELETE SET NULL" in statements
    )


def test_upgrade_never_rewrites_existing_rows():
    """The backfill rule is the default: old rows keep both ids null."""
    migration = _load_migration()

    statements = _capture(migration.upgrade)

    assert not [statement for statement in statements if "UPDATE" in statement.upper()]


def test_downgrade_drops_exactly_what_upgrade_added():
    migration = _load_migration()

    statements = _capture(migration.downgrade)

    assert statements == [
        (
            "ALTER TABLE flow_execution DROP CONSTRAINT "
            + "fk_flow_execution_parent_execution_id"
        ),
        "DROP INDEX ix_flow_execution_root_execution_id",
        "DROP INDEX ix_flow_execution_parent_execution_id",
        "ALTER TABLE flow_execution DROP COLUMN delegation_depth",
        "ALTER TABLE flow_execution DROP COLUMN root_execution_id",
        "ALTER TABLE flow_execution DROP COLUMN parent_execution_id",
    ]
