"""Apply every revision to an empty database, one transaction per revision.

`test_alembic_single_head.py` only walks the revision graph; nothing there runs
`upgrade()`. This does, against a throwaway database, because the change that
made a release safe against live traffic (each revision commits on its own)
also changes what a revision may assume: it can no longer see uncommitted work
from the revision before it, and a `SET LOCAL` no longer leaks into its
successors.

The commit count is the assertion that matters. Applying the whole batch in one
transaction is what held an ACCESS EXCLUSIVE lock on `account` from an early
revision until a later revision reached `user`, which is a deadlock against any
request that takes those two in the other order.
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from preloop.models.migration_runtime import connect_args

REPO_BACKEND = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_BACKEND / "preloop" / "models"


@pytest.fixture
def throwaway_database() -> Iterator[str]:
    """Create an empty database for this test and drop it afterwards."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is not set")
    url = make_url(database_url)
    name = f"preloop_migration_smoke_{secrets.token_hex(6)}"
    maintenance = create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with maintenance.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # pragma: no cover - depends on local privileges
        maintenance.dispose()
        pytest.skip(f"cannot create a throwaway database: {exc}")
    fresh = url.set(database=name)
    try:
        target = create_engine(fresh, isolation_level="AUTOCOMMIT")
        with target.connect() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        target.dispose()
        yield fresh.render_as_string(hide_password=False)
    finally:
        with maintenance.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        maintenance.dispose()


def _commits(url: str, database: str) -> int:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT xact_commit FROM pg_stat_database WHERE datname = :name"),
                {"name": database},
            ).scalar()
    finally:
        engine.dispose()
    return int(value or 0)


def test_upgrade_head_commits_each_revision(throwaway_database: str) -> None:
    url = make_url(throwaway_database)
    database = url.database
    assert database is not None
    before = _commits(throwaway_database, database)

    environment = dict(os.environ)
    environment["DATABASE_URL"] = throwaway_database
    environment["PRELOOP_DISABLE_TELEMETRY"] = "true"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_BACKEND), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # A subprocess, because env.py reads DATABASE_URL at import time and the
    # deployed hook runs it exactly this way.
    completed = subprocess.run(
        [sys.executable, "-m", "preloop.models.migrate"],
        cwd=str(MODELS_DIR),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    engine = create_engine(throwaway_database)
    try:
        with engine.connect() as connection:
            heads = [
                row[0]
                for row in connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            ]
            # Spot-check the tables the incident's revisions touched.
            columns = connection.execute(
                text(
                    "SELECT table_name || '.' || column_name FROM "
                    "information_schema.columns WHERE table_schema = 'public'"
                )
            ).scalars()
            column_names = set(columns)
    finally:
        engine.dispose()

    assert len(heads) == 1
    # Both tables the deadlock formed on, plus the rename the last
    # revision applies to the column the deadlocked ALTER TABLE added.
    assert "user.plan_choice_made_at" in column_names
    assert "flow_runner.ephemeral" in column_names
    assert "account.billing_pending_change" in column_names

    after = _commits(throwaway_database, database)
    # One transaction for the whole batch would be a handful of commits. One
    # per revision is well over a hundred, and it is the per-revision commit
    # that releases each revision's locks before the next one starts.
    assert after - before > 100


def test_the_migration_session_asks_for_a_short_lock_timeout(
    throwaway_database: str,
) -> None:
    """The setting has to reach Postgres, not just the Python config object."""
    engine = create_engine(
        throwaway_database, connect_args=connect_args(throwaway_database)
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SHOW lock_timeout")).scalar() == "5s"
    finally:
        engine.dispose()
