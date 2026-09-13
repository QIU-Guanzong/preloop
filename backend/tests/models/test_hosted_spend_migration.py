"""Hosted migration creates durable tables without inventing existing balances."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select

from preloop.models import models


def test_upgrade_creates_ledger_shape_but_no_existing_account_credit(db_session):
    account = models.Account(organization_name="pre-existing-unverified")
    db_session.add(account)
    db_session.flush()
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20260912_hosted_spend.py"
    )
    spec = importlib.util.spec_from_file_location("hosted_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        migration.upgrade()
    assert db_session.execute(select(models.HostedSpendAccount.id)).first() is None
    for model in (
        models.HostedSpendAccount,
        models.HostedSpendMonth,
        models.HostedSpendReservation,
    ):
        actual = {
            column["name"]
            for column in inspect(db_session.connection()).get_columns(
                model.__tablename__
            )
        }
        assert actual == {column.name for column in model.__table__.columns}
