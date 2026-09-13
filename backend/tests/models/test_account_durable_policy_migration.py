"""Exercise additive policy/billing backfill against actual PostgreSQL DDL."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, select

from preloop.models import models


def load_migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20260912_account_durable_policy.py"
    )
    spec = importlib.util.spec_from_file_location("account_durable_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("history", [183, 730, -1, None, True, "730", 0, -2, 2**40])
def test_additive_backfill_preserves_metadata_and_valid_promises(db_session, history):
    metadata = {
        "subscription_history_retention_days": history,
        "billing_seat_sync_pending": True,
        "billing_seat_sync_generation": "00000000-0000-0000-0000-000000000001",
        "billing_seat_sync_attempted_at": "2026-09-12T09:30:00+00:00",
        "billing_pending_change": {"plan": "team", "operation": "preserve"},
        "unrelated": {"keep": True},
    }
    account = models.Account(organization_name="migration-backfill", meta_data=metadata)
    db_session.add(account)
    db_session.flush()
    account_id = account.id
    migration = load_migration()
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        migration.upgrade()
    db_session.expire_all()
    account = db_session.execute(
        select(models.Account).where(models.Account.id == account_id)
    ).scalar_one()
    expected = (
        history
        if type(history) is int and (history == -1 or 0 < history < 2**31)
        else None
    )
    assert account.subscription_history_retention_days == expected
    assert account.meta_data == metadata
    assert account.billing_seat_sync_pending is True
    assert (
        account.billing_seat_sync_generation == metadata["billing_seat_sync_generation"]
    )
    assert account.billing_seat_sync_attempted_at == datetime(
        2026, 9, 12, 9, 30, tzinfo=UTC
    )
    assert account.billing_pending_change == metadata["billing_pending_change"]
    columns = {
        column["name"]: column
        for column in inspect(db_session.connection()).get_columns("account")
    }
    assert columns["billing_seat_sync_pending"]["nullable"] is False
    assert columns["subscription_history_retention_days"]["nullable"] is True


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        "invalid",
        {
            "billing_seat_sync_attempted_at": "broken",
            "billing_seat_sync_generation": "x" * 37,
            "billing_pending_change": [],
        },
    ],
)
def test_malformed_optional_legacy_state_is_not_invented(metadata):
    values = load_migration()._backfill_values(metadata)
    assert values == {
        "subscription_history_retention_days": None,
        "billing_seat_sync_pending": False,
        "billing_seat_sync_generation": None,
        "billing_seat_sync_attempted_at": None,
        "billing_pending_change": None,
    }


def test_ordinary_account_is_not_updated_by_backfill(db_session):
    from sqlalchemy import event

    account = models.Account(
        organization_name="no-policy-metadata", meta_data={"unrelated": True}
    )
    db_session.add(account)
    db_session.flush()
    account_id = account.id
    updates = []
    connection = db_session.connection()

    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE account"):
            updates.append(parameters)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        with Operations.context(MigrationContext.configure(connection)):
            load_migration().downgrade()
            load_migration().upgrade()
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert not any(account_id in params.values() for params in updates)
    db_session.expire_all()
    assert (
        db_session.get(models.Account, account_id).subscription_history_retention_days
        is None
    )
