"""Real concurrent transactions prove hosted allowances cannot overspend."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import hosted_spend as ledger

NOW = datetime(2026, 9, 12, tzinfo=UTC)


@pytest.fixture
def account_id(db_engine):
    identifier = uuid4()
    with Session(db_engine) as db:
        db.add(models.Account(id=identifier, organization_name="hosted-ledger-test"))
        db.commit()
    yield identifier
    with Session(db_engine) as db:
        db.execute(delete(models.Account).where(models.Account.id == identifier))
        db.commit()


def baseline(db_engine, account_id, lifetime="0", monthly="0"):
    with Session(db_engine) as db:
        ledger.establish_baseline(
            db,
            account_id=account_id,
            lifetime_spent=lifetime,
            month_spent=monthly,
            now=NOW,
            evidence="isolated test account creation",
        )
        db.commit()


def reserve(
    db, account_id, amount="0.3", lifetime="0.5", monthly=None, now=NOW, key=None
):
    return ledger.reserve(
        db,
        account_id=account_id,
        operation_key=key or str(uuid4()),
        amount=amount,
        lifetime_limit=lifetime,
        monthly_limit=monthly,
        now=now,
    )


def test_existing_unknown_balance_is_never_fresh_credit(db_engine, account_id):
    with Session(db_engine) as db:
        assert (
            ledger.snapshot(db, account_id=account_id, now=NOW)["lifetime_spent"]
            is None
        )
        with pytest.raises(
            ledger.HostedSpendUnavailableError, match="not been verified"
        ):
            reserve(db, account_id)


def test_two_concurrent_requests_cannot_spend_same_credit(db_engine, account_id):
    baseline(db_engine, account_id)
    barrier = Barrier(2)

    def attempt():
        with Session(db_engine) as db:
            barrier.wait(timeout=5)
            try:
                reserve(db, account_id)
                db.commit()
                return "reserved"
            except ledger.HostedSpendExceededError:
                db.rollback()
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["denied", "reserved"]
    with Session(db_engine) as db:
        assert ledger.snapshot(db, account_id=account_id, now=NOW)[
            "lifetime_reserved"
        ] == Decimal("0.3")


def test_settlement_release_and_recovery_are_durable_and_idempotent(
    db_engine, account_id
):
    baseline(db_engine, account_id)
    with Session(db_engine) as db:
        row = reserve(db, account_id)
        identifier = row.id
        ledger.mark_dispatched(db, account_id=account_id, reservation_id=identifier)
        db.commit()
    with Session(db_engine) as db:
        ledger.settle(db, account_id=account_id, reservation_id=identifier, actual=None)
        db.commit()
        assert (
            db.get(models.HostedSpendReservation, identifier).status
            == "recovery_required"
        )
        assert ledger.snapshot(db, account_id=account_id, now=NOW)[
            "lifetime_reserved"
        ] == Decimal("0.3")
        ledger.settle(
            db, account_id=account_id, reservation_id=identifier, actual="0.12"
        )
        db.commit()
        ledger.settle(
            db, account_id=account_id, reservation_id=identifier, actual="0.12"
        )
        db.commit()
        result = ledger.snapshot(db, account_id=account_id, now=NOW)
        assert result["lifetime_spent"] == Decimal("0.12")
        assert result["lifetime_reserved"] == 0
        with pytest.raises(ledger.HostedSpendUnavailableError, match="Conflicting"):
            ledger.settle(
                db, account_id=account_id, reservation_id=identifier, actual="0.11"
            )
        db.rollback()
        next_row = reserve(db, account_id)
        ledger.settle(
            db,
            account_id=account_id,
            reservation_id=next_row.id,
            actual=None,
            proven_not_dispatched=True,
        )
        db.commit()
        assert (
            ledger.snapshot(db, account_id=account_id, now=NOW)["lifetime_reserved"]
            == 0
        )


def test_unknown_dispatched_cancellation_never_releases_credit(db_engine, account_id):
    baseline(db_engine, account_id)
    with Session(db_engine) as db:
        row = reserve(db, account_id)
        ledger.mark_dispatched(db, account_id=account_id, reservation_id=row.id)
        ledger.settle(
            db,
            account_id=account_id,
            reservation_id=row.id,
            actual=None,
            proven_not_dispatched=True,
        )
        db.commit()
        assert row.status == "recovery_required"
        assert ledger.snapshot(db, account_id=account_id, now=NOW)[
            "lifetime_reserved"
        ] == Decimal("0.3")


def test_monthly_allowance_resets_in_utc_and_settles_original_period(
    db_engine, account_id
):
    baseline(db_engine, account_id)
    with Session(db_engine) as db:
        first = reserve(db, account_id, amount="2", lifetime=None, monthly="2")
        ledger.mark_dispatched(db, account_id=account_id, reservation_id=first.id)
        db.commit()
        # Settlement after midnight belongs to the reservation's original month.
        next_month = datetime(2026, 10, 1, tzinfo=UTC)
        second = reserve(
            db, account_id, amount="2", lifetime=None, monthly="2", now=next_month
        )
        ledger.settle(db, account_id=account_id, reservation_id=first.id, actual="2")
        db.commit()
        result = ledger.snapshot(db, account_id=account_id, now=next_month)
        assert result["month_spent"] == 0
        assert result["month_reserved"] == 2
        assert result["lifetime_spent"] == 2
        assert second.period.month == 10
        with pytest.raises(ledger.HostedSpendExceededError):
            reserve(db, account_id, lifetime=None, monthly="2", now=next_month)


def test_downgrade_and_history_deletion_cannot_reset_lifetime_credit(
    db_engine, account_id
):
    baseline(db_engine, account_id, lifetime="1", monthly="0")
    with Session(db_engine) as db:
        db.execute(
            delete(models.ApiUsage).where(models.ApiUsage.account_id == account_id)
        )
        db.commit()
        with pytest.raises(ledger.HostedSpendExceededError):
            reserve(db, account_id, now=datetime(2026, 10, 1, tzinfo=UTC))
        db.rollback()
        with pytest.raises(ledger.HostedSpendUnavailableError, match="already exists"):
            ledger.establish_baseline(
                db,
                account_id=account_id,
                lifetime_spent=0,
                month_spent=0,
                now=NOW,
                evidence="cannot reset",
            )


def test_replayed_key_and_other_account_cannot_dispatch_or_settle(
    db_engine, account_id
):
    baseline(db_engine, account_id)
    with Session(db_engine) as db:
        row = reserve(db, account_id, key="one-request")
        identifier = row.id
        db.commit()
        with pytest.raises(ledger.HostedSpendUnavailableError, match="already exists"):
            reserve(db, account_id, key="one-request")
        db.rollback()
        with pytest.raises(ledger.HostedSpendUnavailableError):
            ledger.settle(db, account_id=uuid4(), reservation_id=identifier, actual=0)


@pytest.mark.parametrize("amount", [None, -1, float("nan"), float("inf"), "unpriced"])
def test_unknown_invalid_prices_are_not_zero(db_engine, account_id, amount):
    baseline(db_engine, account_id)
    with Session(db_engine) as db, pytest.raises(ledger.HostedSpendUnavailableError):
        reserve(db, account_id, amount=amount)


def test_verified_month_does_not_invent_unknown_lifetime_balance(db_engine, account_id):
    baseline(db_engine, account_id, lifetime=None, monthly="0.8")
    with Session(db_engine) as db:
        reserve(db, account_id, amount="1", lifetime=None, monthly="2")
        db.commit()
        with pytest.raises(ledger.HostedSpendUnavailableError, match="Lifetime"):
            reserve(db, account_id)
        assert (
            ledger.snapshot(db, account_id=account_id, now=NOW)["lifetime_spent"]
            is None
        )
