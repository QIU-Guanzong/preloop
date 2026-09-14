"""Verified recovery settles a held charge and its audit in one transaction."""

from datetime import UTC, datetime
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import hosted_spend as ledger

NOW = datetime(2026, 9, 14, tzinfo=UTC)


@pytest.fixture
def db_session(db_engine):
    # Preserve the seeded baseline while exercising real caller rollback.
    with db_engine.connect() as connection:
        outer = connection.begin()
        with Session(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        outer.rollback()


@pytest.fixture
def held(db_session, test_user):
    account_id = test_user.account_id
    ledger.establish_baseline(
        db_session,
        account_id=account_id,
        lifetime_spent=0,
        month_spent=0,
        now=NOW,
        evidence="test",
    )
    reservation = ledger.reserve(
        db_session,
        account_id=account_id,
        operation_key="charged",
        amount="0.4",
        lifetime_limit="0.5",
        monthly_limit=None,
        now=NOW,
    )
    ledger.mark_dispatched(
        db_session, account_id=account_id, reservation_id=reservation.id
    )
    ledger.settle(
        db_session, account_id=account_id, reservation_id=reservation.id, actual=None
    )
    db_session.commit()
    return account_id, reservation.id


def recover(db, held, **kwargs):
    return ledger.recover_verified_cost(
        db,
        account_id=held[0],
        reservation_id=held[1],
        evidence="Provider invoice line checked",
        actual=kwargs.pop("actual", "0.2"),
        **kwargs,
    )


def audits(db, held):
    return list(
        db.scalars(
            select(models.BillingOperation).where(
                models.BillingOperation.account_id == held[0],
                models.BillingOperation.kind == "hosted_recovery",
            )
        )
    )


def test_preview_holds_cost_without_writes_or_locks(db_session, held, monkeypatch):
    before = ledger.snapshot(db_session, account_id=held[0], now=NOW)
    monkeypatch.setattr(
        ledger, "lock_account", lambda *args: pytest.fail("preview locked account")
    )
    result = recover(db_session, held)
    assert result["status"] == "would_settle"
    assert ledger.snapshot(db_session, account_id=held[0], now=NOW) == before
    assert audits(db_session, held) == []


@pytest.mark.parametrize("actual", ["0", "0.2", "0.6"])
def test_verified_charge_settles_once_with_original_evidence(db_session, held, actual):
    result = recover(db_session, held, apply=True, actual=actual)
    db_session.commit()
    assert result["status"] == "settled"
    after = ledger.snapshot(db_session, account_id=held[0], now=NOW)
    assert after["lifetime_reserved"] == 0
    assert after["month_reserved"] == 0
    assert after["lifetime_spent"] == Decimal(actual)
    assert after["month_spent"] == Decimal(actual)
    repeated = ledger.recover_verified_cost(
        db_session,
        account_id=held[0],
        reservation_id=held[1],
        actual=actual,
        evidence="Later evidence must not erase original",
        apply=True,
    )
    assert repeated["status"] == "already_recovered"
    assert ledger.snapshot(db_session, account_id=held[0], now=NOW) == after
    assert len(audits(db_session, held)) == 1
    assert (
        audits(db_session, held)[0].payload["evidence"]
        == "Provider invoice line checked"
    )
    assert audits(db_session, held)[0].status == "completed"


def test_conflicting_cost_never_changes_settled_balance(db_session, held):
    recover(db_session, held, apply=True)
    db_session.commit()
    before = ledger.snapshot(db_session, account_id=held[0], now=NOW)
    with pytest.raises(ledger.HostedSpendUnavailableError, match="Conflicting"):
        recover(db_session, held, actual="0.3", apply=True)
    db_session.rollback()
    assert ledger.snapshot(db_session, account_id=held[0], now=NOW) == before
    assert len(audits(db_session, held)) == 1


def test_rollback_reverts_both_settlement_and_audit(db_session, held):
    before = ledger.snapshot(db_session, account_id=held[0], now=NOW)
    recover(db_session, held, apply=True)
    db_session.rollback()
    assert ledger.snapshot(db_session, account_id=held[0], now=NOW) == before
    assert audits(db_session, held) == []


def test_audit_flush_failure_rolls_back_charge(db_session, held, monkeypatch):
    before = ledger.snapshot(db_session, account_id=held[0], now=NOW)
    original = db_session.flush

    def flush(*args, **kwargs):
        if any(isinstance(row, models.BillingOperation) for row in db_session.new):
            raise RuntimeError("audit storage failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", flush)
    with pytest.raises(RuntimeError, match="audit storage"):
        recover(db_session, held, apply=True)
    db_session.rollback()
    assert ledger.snapshot(db_session, account_id=held[0], now=NOW) == before
    assert audits(db_session, held) == []


@pytest.mark.parametrize("apply", [False, True])
def test_wrong_account_cannot_preview_or_settle(db_session, held, apply):
    with pytest.raises(ledger.HostedSpendUnavailableError):
        recover(db_session, (uuid.uuid4(), held[1]), apply=apply)
    assert ledger.snapshot(db_session, account_id=held[0], now=NOW)[
        "lifetime_reserved"
    ] == Decimal("0.4")


@pytest.mark.parametrize("actual", [None, "NaN", "Infinity", "-1"])
def test_unknown_or_invalid_cost_never_releases(db_session, held, actual):
    with pytest.raises(ledger.HostedSpendUnavailableError):
        recover(db_session, held, actual=actual, apply=True)
    assert ledger.snapshot(db_session, account_id=held[0], now=NOW)[
        "lifetime_reserved"
    ] == Decimal("0.4")


def test_already_settled_without_operator_audit_is_reported_honestly(db_session, held):
    ledger.settle(db_session, account_id=held[0], reservation_id=held[1], actual="0.2")
    result = recover(db_session, held, apply=True)
    assert result["status"] == "already_settled_without_recovery_audit"
    assert audits(db_session, held) == []


def test_account_scoped_bounded_listing_and_cursor(db_session, held):
    rows = ledger.unresolved_reservations(db_session, account_id=held[0], limit=1)
    assert len(rows) == 1
    assert rows[0]["reservation_id"] == str(held[1])
    assert set(rows[0]) == {
        "reservation_id",
        "operation_key",
        "created_at",
        "updated_at",
        "status",
        "reserved_usd",
        "period",
    }
    assert (
        ledger.unresolved_reservations(db_session, account_id=held[0], after_id=held[1])
        == []
    )
    assert ledger.unresolved_reservations(db_session, account_id=uuid.uuid4()) == []
    with pytest.raises(ValueError):
        ledger.unresolved_reservations(db_session, account_id=held[0], limit=101)
    recover(db_session, held, apply=True)
    assert ledger.unresolved_reservations(db_session, account_id=held[0]) == []


def _reserve(db, account_id, key):
    return ledger.reserve(
        db,
        account_id=account_id,
        operation_key=key,
        amount="0.01",
        lifetime_limit="50",
        monthly_limit=None,
        now=NOW,
    )


def test_fleet_unresolved_reservation_count_excludes_settled_and_does_not_mutate(
    db_session, test_user
):
    account_id = test_user.account_id
    ledger.establish_baseline(
        db_session,
        account_id=account_id,
        lifetime_spent=0,
        month_spent=0,
        now=NOW,
        evidence="fleet-count",
    )
    before = ledger.count_unresolved_reservations(db_session)
    reserved = _reserve(db_session, account_id, "fleet-reserved")
    dispatched = _reserve(db_session, account_id, "fleet-dispatched")
    ledger.mark_dispatched(
        db_session, account_id=account_id, reservation_id=dispatched.id
    )
    recovery = _reserve(db_session, account_id, "fleet-recovery")
    ledger.mark_dispatched(
        db_session, account_id=account_id, reservation_id=recovery.id
    )
    ledger.settle(
        db_session, account_id=account_id, reservation_id=recovery.id, actual=None
    )
    settled = _reserve(db_session, account_id, "fleet-settled")
    ledger.settle(
        db_session, account_id=account_id, reservation_id=settled.id, actual="0.01"
    )
    statuses = {
        str(row.id): row.status
        for row in db_session.scalars(select(models.HostedSpendReservation))
    }
    result = ledger.count_unresolved_reservations(db_session)
    if not before["truncated"]:
        assert result["count"] == before["count"] + 3
        assert result["truncated"] is False
    assert statuses == {
        str(row.id): row.status
        for row in db_session.scalars(select(models.HostedSpendReservation))
    }
    assert reserved.status == "reserved"
    assert dispatched.status == "dispatched"
    assert recovery.status == "recovery_required"
    assert settled.status == "settled"
    with pytest.raises(ValueError):
        ledger.count_unresolved_reservations(db_session, limit=0)
    with pytest.raises(ValueError):
        ledger.count_unresolved_reservations(db_session, limit=102)


def test_fleet_unresolved_reservation_count_uses_101st_sentinel(db_session, test_user):
    account_id = test_user.account_id
    ledger.establish_baseline(
        db_session,
        account_id=account_id,
        lifetime_spent=0,
        month_spent=0,
        now=NOW,
        evidence="fleet-sentinel",
    )
    existing = ledger.count_unresolved_reservations(db_session)
    needed = 0 if existing["truncated"] else max(0, 101 - existing["count"])
    for index in range(needed):
        _reserve(db_session, account_id, f"fleet-sentinel-{index}")
    result = ledger.count_unresolved_reservations(db_session)
    assert result["truncated"] is True
    assert result["count"] == 100
